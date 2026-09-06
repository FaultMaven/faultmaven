"""Reset the Knowledge Base: wipe SQL + ChromaDB state, then trigger re-bootstrap.

When to use
-----------
Run this after pulling a KB-related bug fix that has invalidated the
ingested vector chunks (chunker change, embedding model change, schema
migration to knowledge_items, etc.), or to recover from a known-bad
state introduced by an earlier ingestion bug.

What it does
------------
1. Deletes every row from ``knowledge_items`` (or filtered by scope).
2. Deletes every row from ``conversion_drafts`` whose source is a
   pre-deployed runbook (so case-generated drafts that users authored
   are preserved). Use ``--all-drafts`` to nuke those too.
3. Removes ChromaDB collections backing global/team/personal KB so
   they're recreated empty on next API start.
4. Optionally invokes the bootstrap directly (``--rebuild``) for
   testing without restarting the API.

Safety
------
Refuses to run without ``--yes`` since the operation is destructive.
Prints a dry-run summary first so you can sanity-check counts.

**Stop the API first.** The wipe ``rmtree``s a ChromaDB directory that a running
server holds open. A live API keeps file handles (and in-memory collection
state) on the tree being deleted, so the process can go on serving reads from
deleted files, recreate a partial directory under the one just removed, or
error on its next write. Scale the API down for the wipe — or, if that is not
possible, restart it immediately afterwards so it reopens a clean store::

    kubectl -n faultmaven scale deploy/faultmaven-api --replicas=0
    # ... run the wipe against the same volume ...
    kubectl -n faultmaven scale deploy/faultmaven-api --replicas=1

The same applies to the Docker Compose stack, where ``data/`` is bind-mounted
into the API container: ``./faultmaven.sh stop``, wipe, then ``start``. Running
it inside the API container with the server up has the identical problem.

Usage (``fm-reset-kb``, installed with the package)
--------------------------------------------------
    source .venv/bin/activate
    fm-reset-kb --dry-run            # See what would change
    fm-reset-kb --yes                # Wipe; bootstrap reruns on API restart
    fm-reset-kb --yes --rebuild      # Wipe + immediate in-process rebuild

In a Kubernetes deployment (standalone/on-prem only — this refuses to run
under ``TENANT_PROVIDER=multi``), run it in the API pod:
    kubectl exec -it deploy/faultmaven-api -- fm-reset-kb --dry-run

Which store it wipes
--------------------
The same one the server opens, asked the same way (fm#936). The wipe is a
``rmtree`` of a LOCAL ChromaDB directory, so three questions are answered
before anything is deleted — all of them from the settings the running server
reads, never from a second, independently-derived path — and any of them can
refuse:

1. *Is a local directory the store at all?* ``CHROMADB_URL`` says no — that is
   the knob ``create_kb_chromadb_client`` dispatches on, and under it this
   command cannot reach the server's collections. It refuses before touching
   SQL rather than deleting every ``knowledge_items`` row and rmtree-ing
   whatever local directory happens to be lying around. ``CHROMADB_HOST``
   alone is a *different* answer: the client factory still returns a local
   ``PersistentClient`` there, and only ``KnowledgeIngester`` goes remote, so
   the local tree really is the store this deployment searches. That case
   proceeds and warns, because refusing it would leave that store with no way
   to be reset over a second store this command never claimed to touch.
2. *Which local directory?* Whatever ``CHROMADB_KB_PERSIST_DIR`` says, read
   exactly as ``create_persistent_client`` reads it — so a relative value
   (including the shipped default ``./data/chroma-kb``) is relative to THIS
   process's working directory. One spelling, shared with the startup
   bootstrap via ``data_init.resolve_kb_chroma_dir``; there is deliberately no
   project-root anchoring, because no consumer resolves it that way and a
   second spelling is the whole bug. **Run it from the same working directory
   the server runs from** — the image's ``WORKDIR`` is ``/app``, and the dev
   scripts ``cd`` to the checkout.
3. *Does that path exist, and is it store-shaped?* Reading the knob is not the
   same as trusting it: it makes the ``rmtree`` argument operator-supplied
   where it used to be bounded by construction. A blank value is refused
   rather than defaulted (no consumer defaults either); a path that does not
   exist means this process is not looking where the server looked; a path
   that is neither empty nor holding ``chroma.sqlite3`` is not a ChromaDB
   store.

``--keep-chroma`` is the explicit opt-out from all of them — nothing is
removed, so none of the questions arise.

Exit codes
----------
| 0 | the run did what it said |
| 1 | refused; **nothing was written** |
| 2 | wiped, then ``--rebuild`` ingested with failures — re-runnable |
| 3 | wiped the SQL rows, could NOT remove the vector store: the KB is
      DIVERGED and needs a human before the API goes back up |

3 exists because the contract had no code for "the destructive half ran and
the recoverable half did not", and the one path that reaches it printed a
DIVERGE warning and then exited 0 — which the documented runbook
(``fm-reset-kb --yes && kubectl … scale --replicas=1``) walks straight past.
It is not 2: there the wipe succeeded and re-running fixes it.

``--dry-run`` writes nothing and therefore always exits **0**, even when it
prints the refusal a real run would give — the exit code reports what happened,
and nothing happened. Read the printed refusal, not the status, when rehearsing.

Refusals (exit 1): no ``--yes``; ``TENANT_PROVIDER=multi``; a ``CHROMADB_URL``
this command cannot wipe; a blank ``CHROMADB_KB_PERSIST_DIR``; a resolved path
that does not exist or is not a ChromaDB store; no knowledge service to
rebuild with.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from sqlalchemy import delete, func, select

from faultmaven.bootstrap.data_init import resolve_kb_chroma_dir

#: argparse's ``description``. A literal, not ``__doc__.splitlines()[0]``:
#: ``python -OO`` strips docstrings, and that expression would raise
#: ``AttributeError: 'NoneType'`` before argparse ever ran.
_SUMMARY = "Reset the Knowledge Base: wipe SQL + ChromaDB state, then re-bootstrap."

#: Exit code for "the destructive half ran and the recoverable half did not".
#:
#: The contract had no code for that state, so the one path that could reach it
#: — a ``rmtree`` that raises after the rows are committed — printed a DIVERGE
#: warning and then exited 0. It is not 2 (``--rebuild`` ingested with
#: failures, where the wipe succeeded and re-running the rebuild fixes it) and
#: it is emphatically not 0: the KB is inconsistent and a human has to remove a
#: directory before the API goes back up. Distinct, because collapsing it into
#: either neighbour is how a runbook gated on ``&&`` walks straight past it.
EXIT_DIVERGED = 3


async def _count_rows(session, model) -> int:
    """COUNT(*) in the database — never materialise the table to measure it."""
    return await session.scalar(select(func.count()).select_from(model)) or 0


def _remote_chroma_refusal(knob: str) -> str:
    """Why a remotely-backed KB cannot be reset by this command.

    Written out rather than inlined because the pre-flight and the ``--dry-run``
    preview must say the SAME thing: a dry run whose message differs from the
    refusal it previews is not a preview.

    ``knob`` names the setting that selected the remote store, spelled as
    ``NAME=value`` — there are two, and an operator who reads "external
    ChromaDB is configured" while looking at an unset ``CHROMADB_URL`` learns
    nothing.
    """
    return (
        "ERROR: refusing to reset the KB.\n"
        "\n"
        f"    {knob} is configured, so this deployment's KB vectors live on a\n"
        "    remote ChromaDB server. This command can only remove a LOCAL\n"
        "    ChromaDB directory, so running it would delete every\n"
        "    knowledge_items row and leave the server's collections fully intact\n"
        "    — SQL and the vector store would DIVERGE, and searches would keep\n"
        "    returning chunks whose rows no longer exist.\n"
        "\n"
        "    Nothing was written.\n"
        "\n"
        "    Options:\n"
        "      * Remove the KB collection on that ChromaDB server, then restart\n"
        "        the API so the bootstrap re-ingests the pack.\n"
        "        `fm-wipe-deployment` (no flags) writes nothing and reports what\n"
        "        collections the configured store actually holds.\n"
        "      * `--keep-chroma` to wipe the SQL rows only, accepting the\n"
        "        divergence above.\n"
        "\n"
        "    If instead the server has silently fallen back to a local store\n"
        "    because the ChromaDB server is unreachable, fix connectivity first:\n"
        "    the fallback tree is per-process and is not the deployment's KB."
    )


def _missing_store_refusal(chroma_dir: Path) -> str:
    """Why a resolved path that does not exist is refused rather than warned about.

    This used to be a warning printed AFTER the SQL wipe, which is the same
    structural defect as everything else fm#936 is about: by the time it fires,
    the irreversible half has run and the operator is reading a description of
    damage. The store is resolved exactly as the server resolves it — the raw
    ``CHROMADB_KB_PERSIST_DIR``, cwd-relative when relative — so "not there"
    means this process is not looking where the server looked, most often
    because it was run from a different working directory.
    """
    return (
        "ERROR: refusing to reset the KB.\n"
        "\n"
        "    No ChromaDB directory exists at the resolved path:\n"
        f"      {chroma_dir}\n"
        "    so there are no vector collections to remove, and deleting the\n"
        "    knowledge_items rows on their own would leave SQL and the vector\n"
        "    store DIVERGED — searches would keep returning chunks whose rows\n"
        "    no longer exist.\n"
        "\n"
        "    Nothing was written.\n"
        "\n"
        "    The path is read exactly as the server reads it (the raw\n"
        "    CHROMADB_KB_PERSIST_DIR, relative to THIS process's working\n"
        "    directory), so this usually means the command was run from\n"
        "    somewhere other than where the server runs. Re-run it there.\n"
        "    Pass --keep-chroma if the KB genuinely has no vector store yet and\n"
        "    you only want the SQL rows gone."
    )


def _looks_like_a_chroma_store(path: Path) -> bool:
    """Whether ``path`` is plausibly a ChromaDB persist directory.

    The wipe target became operator-supplied when it started coming from
    ``CHROMADB_KB_PERSIST_DIR`` (fm#936). Before that it was
    ``<project root>/data/chroma-kb`` and bounded by construction; now a
    mistyped, blank or over-broad value is a ``rmtree`` argument. Two shapes
    are accepted, and nothing else:

    * it holds ``chroma.sqlite3`` — every persist directory does, at the
      ``>= 0.5.3`` floor this project pins; or
    * it is empty — a store the bootstrap has created and nothing has written
      to yet, which is a legitimate thing to be asked to remove.

    A path that is neither is not a store: ``/``, ``/app``, a home directory,
    the checkout. Refusing those costs an operator one corrected flag; being
    wrong the other way costs them the directory.
    """
    if (path / "chroma.sqlite3").exists():
        return True
    try:
        return not any(path.iterdir())
    except OSError:
        # Not a directory, or unreadable. Either way it is not a store, and
        # "cannot tell" must answer no on a path about to be deleted.
        return False


def _not_a_store_refusal(chroma_dir: Path) -> str:
    """Why a resolved path that is not a ChromaDB store is not wiped.

    The remedy names ``CHROMADB_KB_PERSIST_DIR`` and the working directory and
    nothing else, because those are the only two inputs left. ``PROJECT_ROOT``
    used to appear here and no longer decides anything — advice on a
    destructive command that sends an operator to change an inert variable and
    re-run into the identical refusal is worse than no advice.
    """
    return (
        "ERROR: refusing to reset the KB.\n"
        "\n"
        "    The resolved ChromaDB path is:\n"
        f"      {chroma_dir}\n"
        "    …and it is neither empty nor a ChromaDB store (no chroma.sqlite3),\n"
        "    so removing it would delete something that is not the KB.\n"
        "\n"
        "    Nothing was written.\n"
        "\n"
        "    Two inputs decide that path: CHROMADB_KB_PERSIST_DIR, and — when it\n"
        "    is relative — the working directory this ran from. Check both\n"
        "    against the server's.\n"
        "\n"
        "    If that IS the store and its chroma.sqlite3 is gone, the directory\n"
        "    is already unusable: remove it by hand and restart the API, which\n"
        "    re-ingests the pack."
    )


def _unusable_knob_refusal(exc: Exception) -> str:
    """Why a blank ``CHROMADB_KB_PERSIST_DIR`` is refused rather than defaulted.

    Substituting the documented default would be a guess no consumer makes —
    they read the attribute, which exists and is blank — so the bootstrap would
    create one tree while the container failed to open another. That is fm#936
    rebuilt inside its own fix, which is why the resolver raises instead.
    """
    return (
        "ERROR: refusing to reset the KB.\n"
        "\n"
        f"    {exc}\n"
        "\n"
        "    Nothing was written. There is no path to wipe, and guessing the\n"
        "    documented default would target a tree this deployment's ChromaDB\n"
        "    client does not open either — it fails on the blank value too.\n"
        "\n"
        "    Fix the variable, then re-run. `--keep-chroma` wipes the SQL rows\n"
        "    alone if that is genuinely what you want."
    )


async def reset_kb(
    dry_run: bool,
    all_drafts: bool,
    rebuild: bool,
    keep_chroma: bool,
) -> int:
    from faultmaven.infrastructure.persistence.database import get_db_session
    from faultmaven.infrastructure.persistence.models import (
        ConversionDraftModel,
        KnowledgeItemModel,
    )
    from faultmaven.providers.tenancy.factory import (
        BUILTIN_MULTI,
        requested_tenant_provider,
    )

    # Multi-tenant DBs hold every tenant's KB; a blanket wipe/rebuild through
    # this script bypasses the audited maintenance path. Refuse — reseed the
    # platform tier via `python -m faultmaven.jobs.run kb_seed
    # --cross-tenant-maintenance` instead (#770).
    if requested_tenant_provider() == BUILTIN_MULTI:
        print(
            "ERROR: reset_kb refuses to run under TENANT_PROVIDER=multi. "
            "Use the audited kb_seed maintenance job to reseed the platform tier."
        )
        return 1

    from faultmaven.bootstrap.data_init import UnusableDataDirError
    from faultmaven.config.deployment_coherence import (
        is_external_chroma_configured,
        is_host_only_chroma_configured,
    )
    from faultmaven.config.settings import get_settings

    settings = get_settings()
    # The store the SERVER opens, resolved from the SERVER's own settings —
    # never re-derived here (fm#936). Every question below is answered from
    # `settings`, so there is no second source that can drift.
    #
    # The gate is `is_external_chroma_configured`, the SAME predicate
    # `create_kb_chromadb_client` dispatches on, because this command must act
    # on the store that factory opens and no other. `CHROMADB_HOST` alone does
    # NOT reach here: under that opt-in the factory still returns a local
    # PersistentClient (measured — `chroma_server_host` is None, the persist
    # directory is the configured tree), and only `KnowledgeIngester` goes
    # remote. Refusing there would leave the store the KB actually searches
    # with no way to reset it, on the strength of a second store this command
    # has never claimed to touch. It is warned about instead, below.
    remote_knob = (
        f"CHROMADB_URL={(settings.database.chromadb_url or '').strip()}"
        if is_external_chroma_configured(settings)
        else None
    )
    split_host = (
        (settings.database.chromadb_host or "").strip()
        if is_host_only_chroma_configured(settings)
        else None
    )

    # PRE-FLIGHT, before the session is even opened. The SQL wipe happens
    # first and is not undoable, so a check that runs after it can only
    # describe the damage: this command used to delete every knowledge_items
    # row, rmtree whatever local directory it had resolved, print "Removed …"
    # and exit 0 while the real store kept every vector.
    #
    # `--keep-chroma` skips every check — it is the explicit "wipe SQL only, I
    # know" opt-out, and none of these questions arise when nothing is removed.
    refusal = None
    chroma_dir = None
    try:
        chroma_dir = resolve_kb_chroma_dir(settings)
    except UnusableDataDirError as exc:
        # A knob set to something no consumer can open. There is no path to
        # print and none to wipe, so there is nothing to do but say why.
        if not keep_chroma:
            refusal = _unusable_knob_refusal(exc)
    if refusal is None and not keep_chroma:
        if remote_knob:
            refusal = _remote_chroma_refusal(remote_knob)
        elif not chroma_dir.exists():
            refusal = _missing_store_refusal(chroma_dir)
        elif not _looks_like_a_chroma_store(chroma_dir):
            refusal = _not_a_store_refusal(chroma_dir)
    if refusal and not dry_run:
        print(refusal)
        return 1

    # One session for the survey and the wipe: the counts an operator reads
    # are then the same snapshot the DELETEs run against.
    async with get_db_session() as session:
        ki_count = await _count_rows(session, KnowledgeItemModel)
        draft_count = await _count_rows(session, ConversionDraftModel)

        print("Current state:")
        print(f"  knowledge_items rows: {ki_count}")
        print(f"  conversion_drafts rows: {draft_count}")
        if remote_knob:
            # Naming the local path here would invite exactly the mistake this
            # refuses: it is not the store, and printing it beside "ChromaDB"
            # is how an operator concludes otherwise.
            print(f"  ChromaDB store: remote server ({remote_knob})")
            print("  ChromaDB store reachable by this command: False")
        elif chroma_dir is None:
            print("  ChromaDB path: (unresolvable — CHROMADB_KB_PERSIST_DIR is blank)")
        else:
            # The RESOLVED path, not the literal 'data/chroma-kb/'. Which tree
            # this is depends on CHROMADB_KB_PERSIST_DIR and the working
            # directory this ran from, and an operator cannot check that the
            # wipe targets the server's store unless the command says which
            # store it found.
            print(f"  ChromaDB path: {chroma_dir}")
            print(f"  ChromaDB path exists: {chroma_dir.exists()}")
        if split_host:
            # Not a refusal: the store this command wipes is the one the client
            # factory opens, and that is local here. But the ingester writes to
            # a server off the same knob, so a second copy of the KB's vectors
            # exists that this command cannot reach — and saying nothing is how
            # an operator concludes the reset was total.
            print(f"  ⚠️  CHROMADB_HOST={split_host} also puts KnowledgeIngester on")
            print("      a REMOTE ChromaDB, which this command does not touch. This")
            print("      deployment's KB vectors live in two places; only the local")
            print("      one is reset here.")
        print()

        if dry_run:
            if refusal:
                print(refusal)
                print()
            print("(dry-run) No changes made.")
            return 0

        # SQL wipe. Report the DELETEs' own rowcounts rather than the counts
        # read above, so the numbers printed are what the database actually did.
        deleted_items = (await session.execute(delete(KnowledgeItemModel))).rowcount
        if all_drafts:
            deleted_drafts = (
                await session.execute(delete(ConversionDraftModel))
            ).rowcount
            print(f"Deleted {deleted_drafts} conversion_drafts rows (--all-drafts).")
        else:
            # Only delete drafts whose source_url marks them as bootstrap-generated.
            # We don't currently mark these, so by default we conservatively
            # KEEP all drafts. Operators with a known-clean drafts table can
            # pass --all-drafts.
            print(
                "Kept conversion_drafts rows (use --all-drafts to remove case-"
                "generated drafts too)."
            )
        await session.commit()
    print(f"Deleted {deleted_items} knowledge_items rows.")

    # ChromaDB wipe
    if keep_chroma and remote_knob:
        print(
            f"Kept the remote ChromaDB store ({remote_knob}) (--keep-chroma) — "
            "this command never touches it."
        )
        print(
            "    SQL and the vector store now DIVERGE: searches can still return "
            "chunks whose rows no longer exist, until the KB is re-ingested over "
            "them (the pack's chunk ids are deterministic, so a restart overwrites "
            "the shipped runbooks' chunks; anything else stays orphaned)."
        )
    elif keep_chroma:
        print(f"Kept ChromaDB collections at {chroma_dir} (--keep-chroma).")
    else:
        # The pre-flight established that this path exists and is store-shaped,
        # so the ordinary outcome is a removal. It can still fail — a symlinked
        # persist dir (rmtree refuses those outright), a read-only or
        # root-owned mount, or a tree that vanished since the check. The SQL
        # rows are already gone by now, so a bare traceback here would leave the
        # operator with a diverged KB and no statement of it. Say it.
        try:
            shutil.rmtree(chroma_dir)
        except OSError as exc:
            print()
            print("❌ ERROR: the ChromaDB directory could not be removed:")
            print(f"      {chroma_dir}")
            print(f"      {type(exc).__name__}: {exc}")
            print("    knowledge_items rows were deleted but NO vector collections")
            print("    were removed, so SQL and the vector store now DIVERGE —")
            print("    searches can still return chunks whose rows no longer exist.")
            print("    Remove that directory by hand, then restart the API so the")
            print("    bootstrap re-ingests the pack. (A symlinked persist")
            print("    directory is the common cause: rmtree refuses symlinks.)")
            print()
            print(f"    Exiting {EXIT_DIVERGED}: do NOT bring the API back up until")
            print("    the directory is gone — it would serve the old vectors for")
            print("    rows that no longer exist.")
            # RETURN, and with a code of its own. Falling through printed
            # "Next step: restart the API server", ran an optional rebuild into
            # a store that still held the old vectors, and exited 0 — the
            # irreversible half done, the recoverable half not, and success
            # reported. The documented runbook (`fm-reset-kb --yes && kubectl
            # scale --replicas=1`) proceeds on that 0.
            return EXIT_DIVERGED
        print(f"Removed {chroma_dir}.")

    if rebuild:
        print()
        print("Rebuilding KB in-process (this may take a minute on first run)...")
        from faultmaven.bootstrap.kb_init import bootstrap_kb
        from faultmaven.container import container
        from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

        await container.initialize()
        knowledge_service = container.get_knowledge_service()
        if knowledge_service is None:
            print("ERROR: knowledge_service unavailable; cannot rebuild.")
            return 1
        result = await bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=get_db_session,
            enterprise_id=SingleTenantProvider.DEFAULT_ENTERPRISE_ID,
        )
        print(f"Bootstrap result: {result!r}")
        if result.failed:
            for path, reason in result.failed:
                print(f"  FAILED {path}: {reason}")
            return 2
    else:
        print()
        print("Next step: restart the API server. Bootstrap will rebuild the KB.")

    return 0


def main() -> None:
    """Console entrypoint (``fm-reset-kb``). Exits with ``reset_kb``'s status."""
    parser = argparse.ArgumentParser(description=_SUMMARY)
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the wipe (required for any destructive action).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print state without modifying anything.",
    )
    parser.add_argument(
        "--all-drafts",
        action="store_true",
        help="Also delete case-generated conversion_drafts (default: keep).",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Trigger bootstrap_kb in-process after wipe (default: rely on API restart).",
    )
    parser.add_argument(
        "--keep-chroma",
        action="store_true",
        help=(
            "Wipe the SQL rows only, leaving the vector store alone (default: "
            "remove it). Also the opt-out that lets the command run against an "
            "external CHROMADB_URL, which it otherwise refuses — accepting that "
            "SQL and the vector store then diverge."
        ),
    )
    args = parser.parse_args()

    if not args.dry_run and not args.yes:
        print("Refusing to run without --yes (or use --dry-run to preview).")
        sys.exit(1)

    # sys.exit inside main() keeps the five CLI modules uniform: every one of
    # them exits from main() rather than returning a code for a caller to
    # forward.
    sys.exit(
        asyncio.run(
            reset_kb(
                dry_run=args.dry_run,
                all_drafts=args.all_drafts,
                rebuild=args.rebuild,
                keep_chroma=args.keep_chroma,
            )
        )
    )


if __name__ == "__main__":
    main()
