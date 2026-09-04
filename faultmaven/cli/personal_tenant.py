"""Retire or re-anchor a personal tenant (#1045, ADR-016 D5/D8).

``fm-provision-sso-org`` brings a company tenant into existence out of band.
Personal tenants are the other shape: with ``SSO_JIT_PERSONAL_TENANT_ENABLED``
on, an SSO identity carrying no IdP organization provisions its own tenant on
first sign-in. Nothing retired one, and that gap has teeth — soft-deleting the
organization by hand fails every later login of that subject closed with no way
back, the enterprise does not cascade with it, a live refresh chain keeps minting
for the dead tenant, and a user re-anchored to a company leaves their personal
tenant dormant for good. This command is the operator's half of that lifecycle.

Two operations
--------------
**retire** takes a personal tenant out of service and decides, with
``--next-login``, what the subject's next org-less sign-in gets:

* ``refuse`` (default) — the account stays anchored to the retired enterprise
  and the login is refused with ``reason=personal_tenant_retired``.
* ``fresh-tenant`` — the retirement **clears the account's anchor**, and the
  next org-less login provisions a brand-new personal tenant.

The whole retirement state is typed columns: ``deleted_at`` on the organization
and the enterprise, ``enterprises.personal_tenant_retirement`` for the operator's
choice, and ``users.enterprise_id`` — nullable since migration 052 — for whether
the account is anchored at all. Nothing is encoded in a settings blob and nothing
is renamed; the slug uniqueness indexes are partial on ``deleted_at IS NULL``, so
a retired tenant keeps its slug and the subject's next tenant derives the same
one.

**re-anchor** moves an account off its personal enterprise onto a named,
operator-provisioned company organization — the operator's version of what a
mapped login now does by itself, for the cases where no mapped login is coming.

**purge-idp-org** removes a provider-side organization by its **explicit** id.
Provisioning creates the IdP organization before the database transaction, so an
attempt that minted one and then failed to commit leaves an organization with no
tenant. Cleaning that up takes the id, deliberately: an id re-derived from the
subject also names whatever tenant that subject holds *now*.

Addressing
----------
A **live** tenant is addressed by ``--subject``, through its binding row. A
**retired or partly-retired** one is addressed by ``--organization-id``: the
binding is one of the first things retirement removes, and rebuilding the address
from a derived slug cannot tell one retired tenant of a subject from another.
Every run prints the organization id, so an interrupted one can be finished.

What is NOT touched
-------------------
**Cases, evidence and knowledge items survive**, and so does the organization row
that owns them: it is soft-deleted, never removed, and never renamed. What a
retired tenant keeps, and for how long, is ADR-014's subject. Neither is
``organization_members``: the membership row of a soft-deleted organization
grants nothing (every login binds and verifies the organization first).

Ordering, and why an interrupted run is finishable
--------------------------------------------------
The steps and their reasons live in one place —
:mod:`faultmaven.infrastructure.persistence.tenant_retirement` — because they are
the ordering constraint, not narration about it. In short: the organization is
fenced **first** and the subject's tokens revoked immediately after, so neither a
callback nor a live refresh chain can still reach the tenant; the binding goes
before the provider calls, so no login can ask the provider to repair a
membership and re-create the organization the next step deletes; the IdP
organization is deleted **by the id its own mapping row records**, before that
mapping row goes, so the derived external id is free for a later tenant; and the
anchor is cleared **last**, because it is the step that lets the subject
provision again.

**Run it with the owner DSN.** Like ``fm-provision-sso-org``, and for the same
reason: it reads and writes rows of a tenant it is taking out of service, across
``organizations`` (RLS-tenanted, migration 018) without binding it. A preflight
verifies the connected role really is RLS-exempt and refuses before any write.

Usage (``fm-personal-tenant``, installed with the package)
----------------------------------------------------------
::

    fm-personal-tenant retire --subject user_01H...
    fm-personal-tenant retire --subject user_01H... --next-login fresh-tenant --apply
    fm-personal-tenant retire --organization-id 8f1c... --apply
    fm-personal-tenant re-anchor --subject user_01H... \\
        --organization-id <company org id> --apply
    fm-personal-tenant purge-idp-org --provider-org-id org_01H... --apply

Dry run is the **default**: with no ``--apply`` the command reads, reports every
side-effect it would apply, and writes nothing on either side.

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
from faultmaven.infrastructure.persistence import account_anchor, tenant_retirement
from faultmaven.infrastructure.persistence.database import get_db_session
from faultmaven.infrastructure.persistence.organization_liveness import (
    organization_is_usable,
)
from faultmaven.infrastructure.persistence.rls_role_guard import (
    assert_provisioning_db_role_bypasses_rls,
)
from faultmaven.infrastructure.persistence.tenant_bootstrap import PROVIDER
from faultmaven.models.rbac import Role
from faultmaven.models.rbac_seed import SYSTEM_ROLE_IDS
from faultmaven.modules.auth.contracts import (
    RETIREMENT_POLICY_FRESH_TENANT,
    RETIREMENT_POLICY_REFUSE,
)
from faultmaven.modules.auth.domain.personal_tenant import (
    personal_key_of_slug,
    personal_tenant_key,
)
from faultmaven.modules.auth.domain.services.jwt_token_generator import (
    account_may_hold_credentials,
)
from faultmaven.modules.auth.exceptions import SSOProvisioningError

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = "Retire or re-anchor a personal tenant provisioned by an SSO login."

#: Nothing matched, or a guard refused. Nothing was written on either side.
#: "No such tenant" lives here rather than under "nothing to do": it is what a
#: mistyped subject or organization id looks like, and reporting a typo as a
#: successful no-op is how an operator concludes work was done that was not.
EXIT_REFUSED = 1

#: The end state already holds — the tenant is retired with this same policy, or
#: the account is already re-anchored. Distinct from 0 so a scripted sweep can
#: tell "this run did the work" from "somebody already had".
EXIT_NOTHING_TO_DO = 3

#: Some steps landed and a later one did not. The one outcome that leaves work
#: outstanding, so it gets its own code; re-running finishes it. Deliberately
#: not 2, which argparse owns.
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


def _require_subject(subject: str | None) -> str | None:
    """Refuse an empty ``--subject`` rather than deriving a key from ``""``.

    The documented ``kubectl exec`` recipe interpolates a shell variable here,
    which is exactly how it arrives empty — and an empty subject used to reach
    the key derivation and raise a ``ValueError`` traceback at an operator.
    """
    if subject is None:
        return None
    if not subject.strip():
        raise _Refused(
            "--subject was given but is empty. Pass the IdP's opaque subject "
            "handle (user_01H…), or omit the flag and pass --organization-id."
        )
    return subject


def _resolve_retirement_provider(injected: Any | None) -> Any:
    """The IdP teardown port, or a refusal naming what is missing.

    Refuses rather than skipping the IdP half. A retirement that left the
    provider's organization standing would keep the derived ``external_id``
    claimed, so the subject could never be given a second tenant — and the
    command would have reported success for a step it did not run.
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


async def _resolve_auth_service() -> Any:
    """The auth service that owns the revocation watermark, or a refusal.

    Reuses ``fm-remove-org-member``'s own refusal for a store that cannot end a
    session — in-process FakeRedis, or no client at all. A retirement that
    reported success while every outstanding token stayed valid would be worse
    than one that refused.
    """
    from faultmaven.cli.remove_org_member import _revocation_store_unusable
    from faultmaven.container import container

    await container.initialize()
    auth_service = container.get_auth_service()
    if auth_service is None:
        raise _Refused(
            "No auth service is available, so the subject's tokens cannot be "
            "revoked. Refusing: a retirement that leaves live tokens minting "
            "for the retired tenant has not retired it."
        )
    store = container.get_service("token_revocation_store")
    if store is None:
        raise _Refused(
            "No token revocation store is wired, so the watermark cannot be "
            "written. Refusing to retire."
        )
    unusable = _revocation_store_unusable(store)
    if unusable is not None:
        raise _Refused(f"Refusing to retire: {unusable}.")
    return auth_service


async def _read_state(organization_id: str):
    async with get_db_session() as session:
        return await tenant_retirement.read_state(
            session, provider=PROVIDER, organization_id=organization_id
        )


async def _resolve_organization_id(
    *, subject: str | None, organization_id: str | None
) -> str:
    """Turn the operator's arguments into one organization id, or refuse.

    ``--subject`` addresses a **live** tenant through its binding row; a retired
    or partly-retired one has no binding and is addressed by id. Naming both is
    a cross-check and the command refuses if they disagree.
    """
    if organization_id and not subject:
        return organization_id

    async with get_db_session() as session:
        binding = await tenant_retirement.find_live_binding(
            session, provider=PROVIDER, provider_user_id=subject
        )
    if binding is None:
        raise _Refused(
            f"No live personal tenant is bound to {PROVIDER} subject "
            f"'{subject}'.\n"
            "   A retirement deletes that binding early, so a tenant that is "
            "already part-retired is addressed by --organization-id (the id "
            "this command prints on every run), not by subject.\n"
            "   Nothing was written."
        )
    if organization_id and organization_id != binding.organization_id:
        raise _Refused(
            f"--subject '{subject}' is bound to organization "
            f"{binding.organization_id}, not {organization_id}. Naming both is "
            "a cross-check, and this is it refusing. Nothing was written."
        )
    return binding.organization_id


def _assert_personal_tenant(state, *, subject: str | None) -> str:
    """Refuse anything that is not a personal tenant, and cross-check the subject.

    The slug of a personal tenant is derived from its subject, so parsing it is
    what distinguishes one from a customer's organization. Retiring a company
    tenant here would soft-delete it and stamp it with a policy that has no
    meaning for it.
    """
    key = personal_key_of_slug(state.organization_slug)
    if key is None:
        raise _Refused(
            f"Organization {state.organization_id} (slug "
            f"'{state.organization_slug}') is not a personal tenant: its slug "
            "was not derived from an IdP subject.\n"
            "   This command retires personal tenants only. A company tenant is "
            "provisioned by fm-provision-sso-org and retired by a different "
            "procedure."
        )
    if subject is not None and personal_tenant_key(PROVIDER, subject) != key:
        raise _Refused(
            f"Organization {state.organization_id} does not belong to subject "
            f"'{subject}': its slug derives from a different subject. Nothing "
            "was written."
        )
    return key


def _describe(state, *, policy_flag: str, accounts: list[str]) -> None:
    print(f"\nOrganization: {state.organization_id}  ({state.organization_slug})")
    print(f"Enterprise:   {state.enterprise_id}  ({state.enterprise_slug})")
    if state.binding is not None:
        print(
            f"Binding:      {state.binding.provider}:"
            f"{state.binding.provider_user_id} → this tenant "
            f"(idp org {state.binding.provider_org_id})"
        )
    else:
        print("Binding:      none (already retired, or never written)")
    print(f"Anchored accounts: {len(accounts)}")
    print(f"Next org-less login for this subject: {policy_flag}")


def _retirement_steps(state, *, policy: str, idp, auth_service, accounts) -> list[Step]:
    """The pending side-effects, in the order they have to happen.

    Only the outstanding ones: a step whose state is already reached is left
    out, which is what lets a finished retirement report "nothing to do" instead
    of a list of no-ops.

    The revocation is the one step with no observable end state — a watermark
    cannot be read back as "already bumped" — so it is included **only when some
    other step is outstanding**. Gating it on the rest is what keeps a re-run of
    a finished retirement a genuine no-op instead of a perpetual one-step plan.
    """
    fence: list[Step] = []
    binding: list[Step] = []
    provider_steps: list[Step] = []
    enterprise: list[Step] = []
    anchor: list[Step] = []

    if not state.organization_retired:

        async def _fence() -> bool:
            async with get_db_session() as session:
                return await tenant_retirement.soft_delete_organization(
                    session, organization_id=state.organization_id
                )

        fence.append(
            Step(
                "organization_soft_deleted",
                f"soft-delete organization {state.organization_id} "
                "(fences every login out of the tenant)",
                _fence,
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

        binding.append(
            Step(
                "binding_deleted",
                "delete the sso_personal_orgs row binding "
                f"{state.binding.provider_user_id} to this tenant",
                _drop_binding,
            )
        )

    provider_org_id = state.mapping_provider_org_id
    if provider_org_id is not None:

        async def _retire_idp() -> bool:
            outcome = await asyncio.to_thread(
                lambda: idp.retire_personal_organization(
                    provider_org_id=provider_org_id
                )
            )
            if outcome.organization_absent:
                print(f"  · idp organization {provider_org_id} was already gone")
                return False
            print(f"  · idp memberships removed: {outcome.memberships_deleted}")
            return outcome.organization_deleted

        provider_steps.append(
            Step(
                "idp_organization_deleted",
                f"delete IdP organization {provider_org_id} and its memberships "
                "(the id this tenant's mapping row records)",
                _retire_idp,
            )
        )

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

        provider_steps.append(
            Step(
                "mapping_deleted",
                f"delete the sso_org_mappings row for {PROVIDER}:{provider_org_id}",
                _drop_mapping,
            )
        )

    if not (state.enterprise_retired and state.retirement_policy == policy):

        async def _retire_enterprise() -> bool:
            async with get_db_session() as session:
                return await tenant_retirement.retire_enterprise(
                    session, enterprise_id=state.enterprise_id, policy=policy
                )

        enterprise.append(
            Step(
                "enterprise_retired",
                f"soft-delete the enterprise and record next-login policy "
                f"'{policy}'",
                _retire_enterprise,
            )
        )

    if policy == RETIREMENT_POLICY_FRESH_TENANT and accounts:

        async def _clear_anchor() -> bool:
            cleared = await account_anchor.clear_anchors_anchored_to(
                state.enterprise_id
            )
            return bool(cleared)

        anchor.append(
            Step(
                "anchor_cleared",
                f"clear the enterprise anchor on {len(accounts)} account(s), so "
                "the next org-less login provisions a fresh personal tenant",
                _clear_anchor,
            )
        )

    outstanding = fence + binding + provider_steps + enterprise + anchor
    if not outstanding:
        return []

    revoke: list[Step] = []
    if accounts:

        async def _revoke() -> bool:
            for user_id in accounts:
                await auth_service.revoke_user_tokens(user_id)
            return True

        revoke.append(
            Step(
                "tokens_revoked",
                f"revoke every outstanding token for {len(accounts)} anchored "
                "account(s) (a live refresh chain outlives the callback)",
                _revoke,
            )
        )

    return fence + revoke + binding + provider_steps + enterprise + anchor


async def _apply(steps: list[Step]) -> int:
    """Run the steps in order, one line per side-effect. Returns the exit code.

    A step that raises stops the run: everything after it depends on it having
    happened, and continuing would produce a report that is wrong about the
    state. Whether that is a refusal or an incomplete run turns on whether
    anything had already been applied — the difference between "re-run when the
    cause is fixed" and "until you re-run, the deployment is half-retired".
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


async def retire(
    *,
    subject: str | None,
    organization_id: str | None,
    next_login: str,
    apply: bool,
    idp: Any | None = None,
    auth_service: Any | None = None,
) -> int:
    """Retire one personal tenant. Returns the process exit code."""
    _print_header("Retire Personal Tenant")
    policy = _NEXT_LOGIN_POLICIES[next_login]

    try:
        subject = _require_subject(subject)
        await _preflight_role()
        provider = _resolve_retirement_provider(idp)
        revoker = (
            auth_service if auth_service is not None else await _resolve_auth_service()
        )
        resolved_id = await _resolve_organization_id(
            subject=subject, organization_id=organization_id
        )
        try:
            state = await _read_state(resolved_id)
        except tenant_retirement.EnterpriseRowMissing as exc:
            raise _Refused(
                f"{exc}.\n"
                "   That is a broken row, not an absent tenant: the retirement "
                "has nowhere to record its policy and no anchor to release. "
                "Repair the enterprise row first. Nothing was written."
            ) from exc
        if state is None:
            raise _Refused(
                f"No organization '{resolved_id}' exists. Check the id — a "
                "retired tenant keeps its organization row, so this is an "
                "absent tenant rather than a finished retirement. Nothing was "
                "written."
            )
        key = _assert_personal_tenant(state, subject=subject)
        accounts = await account_anchor.accounts_anchored_to(state.enterprise_id)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    _describe(state, policy_flag=next_login, accounts=accounts)
    print(f"Derived key:  {key}")

    steps = _retirement_steps(
        state, policy=policy, idp=provider, auth_service=revoker, accounts=accounts
    )

    if not steps:
        print(
            "\nAlready retired, with the same next-login policy — nothing to "
            "do. Nothing was written."
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
        print(
            f"\n   Finish this retirement with:\n"
            f"     fm-personal-tenant retire --organization-id "
            f"{state.organization_id} --next-login {next_login} --apply"
        )
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
    if not account_may_hold_credentials(user):
        # THE credential rule, one copy (``jwt_token_generator``). A deactivated
        # account moved into a company organization is a member that cannot sign
        # in and a membership row nobody expects.
        raise _Refused(
            f"Account {user.user_id} may not hold credentials (deactivated or "
            "deleted), so moving its anchor would grant a company organization "
            "a member that cannot sign in. Nothing was written."
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
    if not organization_is_usable(organization):
        # The same predicate the login's bind-and-verify tail uses, so the
        # command cannot accept a tenant a login would refuse.
        raise _Refused(
            f"No usable organization '{organization_id}' (it is an id, not a "
            "slug; a soft-deleted or inactive one does not resolve). Nothing "
            "was written."
        )

    async with get_db_session() as session:
        state = await tenant_retirement.read_state(
            session, provider=PROVIDER, organization_id=organization_id
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


async def reanchor(*, subject: str, organization_id: str, apply: bool) -> int:
    """Move one account off its personal enterprise. Returns the exit code."""
    _print_header("Re-anchor Personal Account")

    try:
        subject = _require_subject(subject)
        await _preflight_role()
        users, user = await _load_user(subject)
        orgs, organization = await _load_company_organization(organization_id)

        async with get_db_session() as session:
            binding = await tenant_retirement.find_live_binding(
                session, provider=PROVIDER, provider_user_id=subject
            )
        current = getattr(user, "enterprise_id", None)
        already_moved = current == organization.enterprise_id
        anchor = await account_anchor.read_anchor(current)
        own_live_personal = binding is not None and binding.enterprise_id == current

        if not already_moved and not (
            anchor.kind is account_anchor.AnchorKind.ABSENT
            or anchor.kind is account_anchor.AnchorKind.RETIRED_PERSONAL
            or own_live_personal
        ):
            # The same narrowing the login makes, made here too: an account
            # moves OFF nothing, off a retirement, or off its own personal
            # tenant. Moving one between company enterprises is a manual
            # migration with different consequences, and is refused.
            raise _Refused(
                f"Account {user.user_id} is anchored to enterprise {current} "
                f"({anchor.kind.value}), which is neither absent, nor a retired "
                "personal tenant, nor its own live one.\n"
                "   This command moves an account OFF its personal tenant only. "
                "Nothing was written."
            )
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    print(f"\nAccount:      {user.username} <{user.email}> ({user.user_id})")
    print(f"Anchored to:  {current} ({anchor.kind.value})")
    print(
        f"Moving to:    {organization.organization_id} ({organization.name}) "
        f"in enterprise {organization.enterprise_id}"
    )

    role_id = SYSTEM_ROLE_IDS[Role.MEMBER]
    existing_role = await orgs.get_member_role(organization_id, user.user_id)

    steps: list[Step] = []
    if not already_moved:

        async def _move() -> bool:
            return await account_anchor.move_account_anchor(
                users,
                user,
                to_enterprise_id=organization.enterprise_id,
                destination_is_personal=False,
                own_live_personal=own_live_personal,
            )

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
                f"grant {user.user_id} the member role in {organization_id}",
                _grant,
            )
        )

    if binding is not None:

        async def _retire_binding() -> bool:
            async with get_db_session() as session:
                return (
                    await tenant_retirement.delete_binding(
                        session, organization_id=binding.organization_id
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
    if binding is not None:
        print(
            "   The personal tenant is now dormant: its cases stay where they "
            "are and nobody can enter it. Retire it when you are ready:\n"
            f"     fm-personal-tenant retire --organization-id "
            f"{binding.organization_id} --apply"
        )
    return 0


# --------------------------------------------------------------------------- #
# purge-idp-org
# --------------------------------------------------------------------------- #


async def purge_idp_org(
    *, provider_org_id: str, apply: bool, idp: Any | None = None
) -> int:
    """Remove one provider-side organization, named explicitly.

    Provisioning creates the IdP organization before the database transaction,
    so an attempt that minted one and then failed to commit leaves an
    organization with no tenant. This is how that residue is cleaned.

    It takes the **id**, never a subject: an id re-derived from a subject also
    names whatever tenant that subject holds now, and deleting *that* is how a
    cleanup becomes an outage.
    """
    _print_header("Purge Provider-Side Organization")
    try:
        provider = _resolve_retirement_provider(idp)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return EXIT_REFUSED

    from faultmaven.infrastructure.persistence.models import SSOOrgMappingModel

    async with get_db_session() as session:
        mapping = await session.get(SSOOrgMappingModel, (PROVIDER, provider_org_id))
    if mapping is not None:
        print(
            f"\n❌ {PROVIDER}:{provider_org_id} is mapped to organization "
            f"{mapping.organization_id}, so it is a LIVE tenant's provider "
            "organization, not residue.\n"
            "   Retire the tenant instead:\n"
            f"     fm-personal-tenant retire --organization-id "
            f"{mapping.organization_id} --apply\n"
            "   Nothing was written."
        )
        return EXIT_REFUSED

    if not apply:
        print(
            f"\nWould apply:\n  · delete IdP organization {provider_org_id} "
            "and its memberships"
        )
        print("\nDry run — nothing was written. Re-run with --apply.")
        return 0

    try:
        outcome = await asyncio.to_thread(
            lambda: provider.retire_personal_organization(
                provider_org_id=provider_org_id
            )
        )
    except SSOProvisioningError as exc:
        print(f"\n❌ {exc}\n   Nothing was written on either side.")
        return EXIT_REFUSED
    if outcome.organization_absent:
        print(f"\nNo IdP organization {provider_org_id} exists. Nothing to do.")
        return EXIT_NOTHING_TO_DO
    print(
        f"\n  ✅ removed IdP organization {provider_org_id} "
        f"(memberships removed: {outcome.memberships_deleted})"
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
        help="The IdP subject (user_01H…) whose LIVE personal tenant to retire",
    )
    retire_parser.add_argument(
        "--organization-id",
        default=None,
        help=(
            "The personal organization's id (an id, not a slug). Required once "
            "a retirement has started, because the subject binding is gone."
        ),
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

    purge_parser = sub.add_parser(
        "purge-idp-org",
        help="Remove a provider-side organization that no tenant claims",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    purge_parser.add_argument(
        "--provider-org-id",
        required=True,
        help="The provider's organization id (org_01H…), named explicitly",
    )
    _add_apply_flags(purge_parser)

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
        if not args.subject and not args.organization_id:
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

    if args.operation == "purge-idp-org":
        sys.exit(
            asyncio.run(
                purge_idp_org(provider_org_id=args.provider_org_id, apply=args.apply)
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
