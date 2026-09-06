"""Move a set of cases from one owner to another inside one enterprise.

Built for the Slack per-workspace cutover (faultmaven-slack-agent#61 step 4,
ADR-012 D10): the global ``slack-agent`` account owns every Slack case opened
before workspaces were bound to their own service accounts, and those cases must
be re-owned by the workspace's account *before* the global one is retired.
``cases.user_id`` is ``ON DELETE SET NULL``, so deleting the old account instead
neither cascades nor blocks — it nulls the owner and drops the cases out of every
owner-scoped view, unrecoverably.

Usage (``fm-reassign-cases``, installed with the package)::

    fm-reassign-cases --enterprise-id <org-id> \\
        --from-user slack-agent --to-user slack-T0B9XNZDR44 \\
        --case-ids-file ids.txt --dry-run

In a Kubernetes deployment, run it in the API pod::

    kubectl exec -i deploy/faultmaven-api -- fm-reassign-cases … --yes

What it writes, in one transaction
----------------------------------
* ``cases.user_id`` — the reassignment itself, **and a ``version`` bump**.
* ``resource_shares`` — one ``case``→``team`` row per case per Team the new owner
  belongs to.
* ``user_audit_log`` — one ``case_reassigned`` row per case.

It writes **nothing** to ``case_messages.author_id`` or
``uploaded_files.uploaded_by``. Those are attribution, not ownership: migration
037 makes ``author_id`` deliberately un-foreign-keyed so that "attribution must
outlive the account it describes", and ADR-011 D5 calls the record
un-backfillable. The turns really were submitted by the old principal; rewriting
them would be falsifying history to make a migration look tidy. Ownership is
current state and moves; authorship is past fact and does not.

Why ``version`` must be bumped
------------------------------
``PostgreSQLHybridCaseRepository`` saves a case with
``UPDATE cases SET user_id = :user_id, … WHERE case_id = :id AND version =
:expected_version`` — it writes the owner back from the in-memory ``Case``. A
turn that loaded a case before this command runs and saves after it would
therefore pass the optimistic-concurrency check and silently restore the old
owner. Bumping ``version`` makes that save miss and raise
``StaleCaseException``, so the reassignment survives.

**The in-flight turn does not.** ``POST /cases/{id}/turns`` deliberately does
not retry on an OCC conflict — "LLM turns are expensive and non-idempotent"
(``modules/case/api/routes.py``) — it returns 409 and the caller decides. So the
bump protects the migration by *discarding* a concurrent turn, which the Slack
agent surfaces as "the case is busy" and the user re-sends. That makes running
with the agent stopped a real instruction, not belt-and-braces.

Why the team share is created rather than carried
-------------------------------------------------
``CaseService._auto_share_slack_case`` resolves Teams from the **owner's**
membership at case-creation time. A global service account that belongs to no
Team therefore produced no share rows at all — so there is nothing to "move",
and a bare owner swap would leave the migrated cases the only Slack cases in the
enterprise that no human can see, while every case created after the bind is
team-visible. Creating the row here is what makes the history match what the
live path now produces.

Why both a file and a sweep
---------------------------
The backend records **no** Slack workspace on a case; the agent's own
``thread_cases`` map is the only place that is written down. So the caller names
the cases (``--case-ids-file``, produced from that map) *and* this command sweeps
the enterprise for everything ``--from-user`` owns — and refuses unless the two
sets are exactly equal. The file alone would silently move nothing on a mistyped
id; the sweep alone would happily merge two workspaces' histories into one
account on a deployment where the global account had served both. Together, a
named-but-not-owned id and an owned-but-unnamed id are each a refusal that names
the ids.

Why the target must be in the enterprise but the source need not be
-------------------------------------------------------------------
``users`` is not tenant-scoped, so both lookups find any account in the
deployment. An unchecked ``--to-user`` is the whole risk here: a mistyped id
would hand a workspace's transcripts to whatever account it resolved to. So the
target must be **anchored to** ``--enterprise-id`` (``users.enterprise_id``,
ADR-017 D3 — the isolation membership, not the billing roster), must be active,
and must not be the source. The **source** is deliberately not held to that: the
account this command exists to retire may already have been moved or may predate
the anchor, and requiring it would refuse the only run this was written for.

Exit codes
----------
| 0 | success, or a dry run |
| 1 | refused, or rolled back — **nothing was written** |
| 2 | argparse usage error (a bad flag), reserved by argparse |

There is no half-state code, unlike ``fm-remove-org-member``: every write here is
in one transaction, so a failure part-way rolls the whole thing back and the
deployment is exactly as it was. A run that is interrupted is re-run, not
finished.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from faultmaven.cli._confirmation import require_confirmation

#: argparse's ``description``. A literal, not derived from ``__doc__``: ``python
#: -OO`` strips docstrings, and that expression would raise before argparse ran.
_SUMMARY = (
    "Reassign a named set of cases to a new owner within one enterprise, "
    "sharing them to the new owner's Teams and recording the move."
)


def _actor() -> str:
    """Best-effort provenance for the audit row's ``details``.

    Not an authenticated principal and never presented as one — this is a CLI,
    and the OS user plus host is the most an operator run can honestly claim.
    It goes in ``details``, never in ``user_id``, so nothing reads it as the
    row's subject.
    """

    import getpass
    import socket

    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # noqa: BLE001 — provenance must not fail a migration
        return "unknown"


class _Refused(Exception):
    """A guard said no. Carries the operator-facing message; nothing written."""


def read_case_ids(path: str) -> list[str]:
    """Parse the case-id file: one id per line, ``#`` comments and blanks skipped.

    Order is preserved so the report reads in the order the operator supplied,
    but duplicates are a **refusal** rather than a silent de-duplication: a file
    assembled from the wrong query is the likeliest way to get one, and the count
    an operator checks against ("14") would otherwise be wrong in the one
    direction that still looks right.
    """
    try:
        raw = Path(path).read_text()
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: a file that is not text at all
        # (the wrong path, a sqlite database, a gzip) is an operator mistake and
        # belongs in the refusal contract, not in a traceback.
        raise _Refused(f"could not read --case-ids-file {path!r}: {exc}") from exc

    ids: list[str] = []
    for line in raw.splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            ids.append(entry)

    if not ids:
        raise _Refused(
            f"--case-ids-file {path!r} names no case ids. Refusing rather than "
            "treating an empty file as 'nothing to do': an empty file is what a "
            "failed extraction looks like."
        )
    counts = Counter(ids)
    duplicates = sorted(i for i, n in counts.items() if n > 1)
    if duplicates:
        raise _Refused(
            f"--case-ids-file {path!r} lists {', '.join(duplicates)} more than "
            "once. Refusing: a duplicate means the file was assembled wrong, and "
            "the id count is the number an operator checks this run against."
        )
    return ids


def describe_set_mismatch(named: list[str], owned: set[str], from_label: str) -> str:
    """Explain exactly how the named set and the swept set differ.

    Both directions matter and mean different things, so both are reported by id
    rather than by count: ids present in the file but not owned point at a
    mistyped or already-moved case, ids owned but unnamed point at a case the
    file's source did not know about — which under a global account is what a
    second Slack workspace's history looks like.
    """
    named_set = set(named)
    lines = []
    unowned = sorted(named_set - owned)
    if unowned:
        lines.append(
            f"  named but NOT owned by {from_label} in this enterprise "
            f"({len(unowned)}): {', '.join(unowned)}"
        )
    unnamed = sorted(owned - named_set)
    if unnamed:
        lines.append(
            f"  owned by {from_label} but NOT named in the file "
            f"({len(unnamed)}): {', '.join(unnamed)}"
        )
    return "\n".join(lines)


async def _resolve_user(user_store, identifier: str):
    """Resolve a username, email, or user id to a user — in that order.

    Same order and same failure posture as ``fm-remove-org-member``: a lookup
    that could not run raises ``UserLookupFailed`` rather than falling through,
    so "no such user" is never reported on evidence this command does not have.
    """
    for lookup in (
        user_store.get_user_by_username,
        user_store.get_user_by_email,
        user_store.get_user,
    ):
        user = await lookup(identifier)
        if user is not None:
            return user
    return None


async def _swept_case_ids(enterprise_id: str, from_user_id: str) -> set[str]:
    """Every case id in this enterprise currently owned by ``from_user_id``.

    ``enterprise_id`` is in the predicate as well as bound into the RLS scope.
    Redundant against ``faultmaven_app`` — the policy already restricts it — but
    this command also has to be correct when run against a connection that is not
    RLS-enforcing, and a sweep that silently spanned tenants is the one error
    that would not announce itself.
    """
    from sqlalchemy import text

    from faultmaven.infrastructure.persistence.database import get_db_session

    async with get_db_session() as session:
        result = await session.execute(
            text(
                "SELECT case_id FROM cases "
                "WHERE user_id = :from_user_id AND enterprise_id = :enterprise_id"
            ),
            {"from_user_id": from_user_id, "enterprise_id": enterprise_id},
        )
        return {row[0] for row in result.fetchall()}


async def _team_ids_in_enterprise(user_id: str, enterprise_id: str) -> list[str]:
    """The user's teams **in this enterprise**, by id.

    ``list_all_user_team_ids`` returns ids only and leans entirely on RLS to
    keep them same-tenant. That is the same assumption ``_swept_case_ids``
    deliberately refuses to make: this command must also be correct on a
    connection that is not RLS-enforcing, and sharing a case to another
    tenant's team is precisely the error that would not announce itself — it
    would print success while making the cases visible to strangers and to
    nobody who should see them. ``list_user_teams`` carries
    ``enterprise_id``, so the predicate can be applied here rather than
    assumed.
    """

    from faultmaven.infrastructure.persistence.sessionless_team_repository import (
        SessionlessTeamRepository,
    )

    teams = await SessionlessTeamRepository().list_user_teams(user_id)
    return [t.team_id for t in teams if t.enterprise_id == enterprise_id]


async def _apply(
    *,
    enterprise_id: str,
    case_ids: list[str],
    from_user_id: str,
    to_user_id: str,
    team_ids: list[str],
    revoke_team_ids: list[str],
    actor: str,
) -> None:
    """Write the reassignment, the shares and the audit rows in one transaction.

    Raises ``_Refused`` if any single UPDATE fails to match, which rolls the
    whole transaction back. The ``AND user_id = :from_user_id`` guard is what
    makes that detection possible: it turns "someone else changed this case
    between the sweep and now" into a matched-zero-rows outcome instead of an
    UPDATE that quietly re-owns a case this run never checked.

    ``revoke_team_ids`` are the teams the OLD owner belonged to and the new one
    does not. Their share rows outlive the owner swap otherwise, and
    ``case_scope_where`` admits any team holding one — so the previous owner's
    teams would keep reading every case this command moved away from them.
    """
    import json
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy import bindparam, text

    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.db_compat import dialect_insert
    from faultmaven.infrastructure.persistence.models import (
        ResourceShareModel,
        UserAuditLogModel,
    )
    from faultmaven.models.interfaces_user import AuditCategory, AuditEventType

    # `enterprise_id` is in the predicate as well as bound into the RLS
    # scope, for the same reason the sweep carries it: this command also has to
    # be correct when run against a connection that is not RLS-enforcing, and a
    # cross-tenant write is the one error that would not announce itself.
    #
    # `updated_at` is set explicitly because the ORM's `onupdate=func.now()`
    # does NOT fire for Core text SQL — without it the row is written while
    # claiming it was not. `last_activity_at` is deliberately left alone: that
    # is the case's activity clock, and re-attribution is not activity.
    reassign = text(
        "UPDATE cases SET user_id = :to_user_id, version = version + 1, "
        "updated_at = :now "
        "WHERE case_id = :case_id AND user_id = :from_user_id "
        "AND enterprise_id = :enterprise_id"
    )

    moved_at = datetime.now(timezone.utc)

    async with get_db_session() as session:
        for case_id in case_ids:
            result = await session.execute(
                reassign,
                {
                    "to_user_id": to_user_id,
                    "case_id": case_id,
                    "from_user_id": from_user_id,
                    "enterprise_id": enterprise_id,
                    "now": moved_at,
                },
            )
            if result.rowcount != 1:
                raise _Refused(
                    f"case {case_id} was owned by the source when this run "
                    f"started but its UPDATE matched {result.rowcount} rows — it "
                    "changed underneath us. The whole transaction has been rolled "
                    "back and NOTHING was written. Re-run: the sweep will "
                    "re-measure and tell you what it now finds."
                )

        # Idempotent by the share table's own unique key, so a re-run after a
        # rollback does not accumulate duplicates. Written with the session's own
        # INSERT rather than through ShareRepository.share(), which commits
        # internally — that would break this transaction into pieces and let a
        # later failure leave shares behind for an owner swap that rolled back.
        for case_id in case_ids:
            for team_id in team_ids:
                await session.execute(
                    dialect_insert(session, ResourceShareModel)
                    .values(
                        share_id=str(uuid.uuid4()),
                        resource_type="case",
                        resource_id=case_id,
                        scope_type="team",
                        scope_id=team_id,
                        enterprise_id=enterprise_id,
                        created_by=to_user_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "resource_type",
                            "resource_id",
                            "scope_type",
                            "scope_id",
                        ]
                    )
                )

        # The owner swap does not move share rows: `case_scope_where` admits any
        # team holding one, so a share to a team the OLD owner belonged to keeps
        # that team reading the case after it has been moved away from them.
        # Only teams the new owner is NOT in are revoked — the overlap is about
        # to be (re)granted above, and dropping it would flap access.
        if revoke_team_ids:
            await session.execute(
                text(
                    "DELETE FROM resource_shares "
                    "WHERE resource_type = 'case' AND scope_type = 'team' "
                    "AND enterprise_id = :enterprise_id "
                    "AND resource_id IN :case_ids AND scope_id IN :team_ids"
                ).bindparams(
                    bindparam("case_ids", expanding=True),
                    bindparam("team_ids", expanding=True),
                ),
                {
                    "enterprise_id": enterprise_id,
                    "case_ids": case_ids,
                    "team_ids": revoke_team_ids,
                },
            )

        # One row per case, not one summary row: `event_type` is indexed and
        # `resource_id` is the natural key an auditor filters on, so "what
        # happened to THIS case" is a row rather than a JSON blob to parse.
        # (`user_audit_log` indexes only (user_id, created_at) and
        # (enterprise_id, created_at) — `resource_id` is queried, not indexed.)
        #
        # `user_id` is None on purpose. It is the row's SUBJECT, and attributing
        # this to the new owner would read as the service account having moved
        # its own cases. A CLI has no authenticated principal to name, and a
        # false actor is worse than none — the operator is recorded in `details`
        # as best-effort provenance instead.
        for case_id in case_ids:
            session.add(
                UserAuditLogModel(
                    user_id=None,
                    enterprise_id=enterprise_id,
                    event_type=AuditEventType.CASE_REASSIGNED.value,
                    event_category=AuditCategory.ADMINISTRATION.value,
                    resource_type="case",
                    resource_id=case_id,
                    details=json.dumps(
                        {
                            "from_user_id": from_user_id,
                            "to_user_id": to_user_id,
                            "shared_to_team_ids": team_ids,
                            "revoked_team_ids": revoke_team_ids,
                            "tool": "fm-reassign-cases",
                            "actor": actor,
                        }
                    ),
                    success=True,
                    created_at=moved_at,
                )
            )


async def reassign_cases(
    *,
    enterprise_id: str,
    from_identifier: str,
    to_identifier: str,
    case_ids_file: str,
    allow_no_team: bool,
    move_unnamed_too: bool,
    dry_run: bool,
) -> int:
    """Run the reassignment. Returns the process exit code."""
    from faultmaven.config.tenant_context import set_current_enterprise_id
    from faultmaven.container import container
    from faultmaven.exceptions import UserLookupFailed
    from faultmaven.infrastructure.persistence.sessionless_enterprise_repository import (
        SessionlessEnterpriseRepository,
    )

    print("=" * 80)
    print("Reassign Cases")
    print("=" * 80)

    try:
        # Parsed before anything is initialised or connected: a bad file is the
        # commonest way to get this wrong, and it costs nothing to find out now.
        named_ids = read_case_ids(case_ids_file)
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return 1

    print("\nInitializing...")
    await container.initialize()

    # RLS scopes cases, resource_shares and teams by `app.current_enterprise_id`. Bind
    # it before opening any session: the engine applies it per transaction from
    # this contextvar, so a session opened first would run unbound (#935).
    set_current_enterprise_id(enterprise_id)

    enterprises = SessionlessEnterpriseRepository()
    enterprise = await enterprises.get_enterprise(enterprise_id)
    if enterprise is None:
        print(
            f"\n❌ No enterprise '{enterprise_id}' is visible.\n"
            "   Check the id (it is an id, not a slug), and note that a deleted "
            "enterprise does not resolve."
        )
        return 1

    user_store = container.get_user_store()
    if user_store is None:
        print("\n❌ Failed to get user store from container")
        return 1

    resolved = {}
    for label, identifier in (
        ("--from-user", from_identifier),
        ("--to-user", to_identifier),
    ):
        try:
            resolved[label] = await _resolve_user(user_store, identifier)
        except UserLookupFailed as exc:
            # Named per argument: "a user lookup failed" sends an operator to
            # check both accounts, and the store's own identifier is the thing
            # that says which one it was actually asked about.
            print(
                f"\n❌ Could not look up {label} "
                f"{getattr(exc, 'identifier', identifier)!r}: the {exc.lookup} "
                "lookup FAILED.\n"
                "   This is not 'no such user' — the store did not answer, so "
                "whether the account exists is unknown and NOTHING has been "
                "written.\n"
                f"   Underlying error: {exc}\n"
                "   Fix the store (check the API logs and the database), then "
                "re-run."
            )
            return 1
    from_user = resolved["--from-user"]
    to_user = resolved["--to-user"]

    for label, identifier, user in (
        ("--from-user", from_identifier, from_user),
        ("--to-user", to_identifier, to_user),
    ):
        if user is None:
            print(
                f"\n❌ No user matches {label} '{identifier}' "
                "(tried username, then email, then user id).\n"
                "   All three lookups completed and matched nothing."
            )
            return 1

    if from_user.user_id == to_user.user_id:
        print(
            f"\n❌ --from-user and --to-user are the same account "
            f"({from_user.username}). There is nothing to move."
        )
        return 1

    # Read the attribute rather than getattr-with-a-default: a user object that
    # does not carry `is_active` is an unrecognised shape, and defaulting it to
    # True would pass exactly the case this guard exists to catch.
    if not getattr(to_user, "is_active", False):
        print(
            f"\n❌ {to_user.username} is not active, so it cannot be given "
            "ownership of these cases — they would be owned by an account that "
            "cannot authenticate, which is the orphaning this command exists to "
            "prevent."
        )
        return 1

    # `users` is not tenant-scoped, so the lookup above found this account
    # wherever it lives. The anchor is what ties it to THIS enterprise.
    if getattr(to_user, "enterprise_id", None) != enterprise_id:
        print(
            f"\n❌ {to_user.username} is not anchored to {enterprise.name} "
            f"({enterprise_id}), so it must not own that enterprise's cases.\n"
            "   This is what a mistyped --to-user looks like, and moving the "
            "cases anyway would hand them to an unrelated account or leave them "
            "owned by someone RLS shows them to nobody as."
        )
        return 1

    owned_ids = await _swept_case_ids(enterprise_id, from_user.user_id)
    named_set = set(named_ids)

    # Split by direction, because the two mismatches mean opposite things.
    #
    # NAMED BUT NOT OWNED is always a mistake — a typo, the wrong organization,
    # or a case already moved. Nothing good comes of proceeding, so it is a hard
    # refusal with no flag.
    unowned = sorted(named_set - owned_ids)
    if unowned:
        print(
            f"\n❌ {len(unowned)} named case id(s) are NOT owned by "
            f"{from_user.username} in this enterprise: {', '.join(unowned)}\n"
            "   Refusing: that is what a typo, a wrong --enterprise-id, or an "
            "already-completed run looks like. Re-derive the file from the "
            "agent's thread_cases map and check the organization id."
        )
        return 1

    # OWNED BUT NOT NAMED is the two-workspace case the cross-check exists to
    # detect: one global account served more than one Slack workspace, and the
    # file describes only one of them. Refusing by default is right — but
    # refusing *unconditionally* would mean the situation this guard was built
    # to catch is the one situation the command can never serve. So it is a
    # deliberate flag, not a general --force: the ids left behind are printed,
    # and they stay with the old account.
    unnamed = sorted(owned_ids - named_set)
    if unnamed and not move_unnamed_too:
        print(
            f"\n❌ {from_user.username} owns {len(unnamed)} case(s) this file "
            f"does not name: {', '.join(unnamed)}\n"
            "   Refusing: under a shared account that is what a SECOND Slack "
            "workspace's history looks like, and moving it into this "
            "workspace's account would merge two customers' investigations.\n"
            "   If you have confirmed those cases belong to a different "
            "workspace and should stay with the old account, re-run with "
            "--leave-unnamed to move only the named ones."
        )
        return 1

    team_ids = await _team_ids_in_enterprise(to_user.user_id, enterprise_id)
    # Teams the OLD owner is in and the new one is not: their share rows would
    # otherwise survive the swap and keep those teams reading the moved cases.
    revoke_team_ids = [
        t
        for t in await _team_ids_in_enterprise(from_user.user_id, enterprise_id)
        if t not in team_ids
    ]
    if not team_ids and not allow_no_team:
        print(
            f"\n❌ {to_user.username} belongs to no Team in this enterprise, "
            "so the moved cases would be shared to nothing and stay owner-only "
            "— invisible to every human, while cases created after the bind are "
            "team-visible.\n"
            "   That is what a bind that did not finish looks like. Fix the Team "
            "membership and re-run, or pass --allow-no-team to move ownership "
            "only, deliberately."
        )
        return 1

    print(f"\nEnterprise:   {enterprise.name} ({enterprise_id})")
    print(f"From:         {from_user.username} ({from_user.user_id})")
    print(f"To:           {to_user.username} ({to_user.user_id})")
    print(f"Cases:        {len(named_ids)} — {', '.join(named_ids)}")
    print(
        "Share to:     "
        + (", ".join(team_ids) if team_ids else "(no Team — --allow-no-team)")
    )
    if revoke_team_ids:
        print(f"Revoke share: {', '.join(revoke_team_ids)} (the old owner's teams)")
    if unnamed:
        print(f"Left behind:  {', '.join(unnamed)} (--leave-unnamed)")
    print(
        "Untouched:    case_messages.author_id, uploaded_files.uploaded_by "
        "(attribution, not ownership), cases.enterprise_id"
    )

    if dry_run:
        print(
            "\nDry run — nothing was written. Re-run with --yes to reassign "
            "these cases."
        )
        return 0

    try:
        await _apply(
            enterprise_id=enterprise_id,
            case_ids=named_ids,
            from_user_id=from_user.user_id,
            to_user_id=to_user.user_id,
            team_ids=team_ids,
            revoke_team_ids=revoke_team_ids,
            actor=_actor(),
        )
    except _Refused as exc:
        print(f"\n❌ {exc}")
        return 1

    print(f"\n✅ {len(named_ids)} case(s) now owned by {to_user.username}.")
    if team_ids:
        print(
            f"✅ Shared to {len(team_ids)} team(s): {', '.join(team_ids)} — the "
            "cases are visible to their members."
        )
    else:
        print(
            "⚠️  No team share written (--allow-no-team): these cases stay owner-only."
        )
    print(f"✅ {len(named_ids)} audit row(s) recorded (case_reassigned).")
    return 0


def main() -> None:
    """Console entrypoint (``fm-reassign-cases``)."""
    parser = argparse.ArgumentParser(
        prog="fm-reassign-cases",
        description=_SUMMARY,
        epilog=(
            "The cases must be named in a file AND be exactly what --from-user "
            "owns in --enterprise-id; the two are cross-checked and any "
            "difference refuses the run. Ownership moves; attribution "
            "(case_messages.author_id, uploaded_files.uploaded_by) does not."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--enterprise-id",
        required=True,
        help="Enterprise the cases live in (an id, not a slug)",
    )
    parser.add_argument(
        "--from-user",
        required=True,
        help="Current owner: username, email address, or user id",
    )
    parser.add_argument(
        "--to-user",
        required=True,
        help=(
            "New owner: username, email address, or user id. Must be an active "
            "account anchored to --enterprise-id"
        ),
    )
    parser.add_argument(
        "--case-ids-file",
        required=True,
        help=(
            "File of case ids to move, one per line ('#' comments allowed). For "
            'the Slack cutover: sqlite3 /app/data/cases.db "SELECT case_id FROM '
            "thread_cases WHERE team_id='T…'\""
        ),
    )
    parser.add_argument(
        "--allow-no-team",
        action="store_true",
        help=(
            "Move ownership even though the new owner belongs to no Team, "
            "leaving the cases owner-only and invisible to humans"
        ),
    )
    parser.add_argument(
        "--leave-unnamed",
        action="store_true",
        help=(
            "Proceed when --from-user owns cases the file does not name, moving "
            "only the named ones and leaving the rest with the old account. For "
            "a shared account that served more than one Slack workspace"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and exit without writing",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the write (required)",
    )
    args = parser.parse_args()

    require_confirmation(
        parser,
        args,
        "This changes who owns these cases and who can see them.",
    )

    sys.exit(
        asyncio.run(
            reassign_cases(
                enterprise_id=args.enterprise_id,
                from_identifier=args.from_user,
                to_identifier=args.to_user,
                case_ids_file=args.case_ids_file,
                allow_no_team=args.allow_no_team,
                move_unnamed_too=args.leave_unnamed,
                dry_run=args.dry_run,
            )
        )
    )
