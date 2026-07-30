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

Working directory
-----------------
The ChromaDB path is resolved under
``faultmaven.bootstrap.data_init.get_project_root()``, which prefers
``PROJECT_ROOT``, then a working directory containing ``alembic.ini`` /
``pyproject.toml``, then the location of ``data_init.py`` itself. Run it from
the project root in development; the image's ``WORKDIR`` (``/app``) holds both
marker files, so the pod resolves the same tree the API writes to.

Exit codes
----------
0 success (or dry-run), 1 refused (no ``--yes``, or ``TENANT_PROVIDER=multi``,
or no knowledge service to rebuild with), 2 the rebuild ingested with failures.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys

from sqlalchemy import delete, func, select

from faultmaven.bootstrap.data_init import get_project_root

#: argparse's ``description``. A literal, not ``__doc__.splitlines()[0]``:
#: ``python -OO`` strips docstrings, and that expression would raise
#: ``AttributeError: 'NoneType'`` before argparse ever ran.
_SUMMARY = "Reset the Knowledge Base: wipe SQL + ChromaDB state, then re-bootstrap."


async def _count_rows(session, model) -> int:
    """COUNT(*) in the database — never materialise the table to measure it."""
    return await session.scalar(select(func.count()).select_from(model)) or 0


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

    project_root = get_project_root()
    chroma_dir = project_root / "data" / "chroma-kb"

    # One session for the survey and the wipe: the counts an operator reads
    # are then the same snapshot the DELETEs run against.
    async with get_db_session() as session:
        ki_count = await _count_rows(session, KnowledgeItemModel)
        draft_count = await _count_rows(session, ConversionDraftModel)

        print("Current state:")
        print(f"  knowledge_items rows: {ki_count}")
        print(f"  conversion_drafts rows: {draft_count}")
        # The RESOLVED path, not the literal 'data/chroma-kb/'. Which tree this
        # is depends on PROJECT_ROOT / the working directory, and an operator
        # cannot check that the wipe targets the server's store unless the
        # command says which store it found.
        print(f"  ChromaDB path: {chroma_dir}")
        print(f"  ChromaDB path exists: {chroma_dir.exists()}")
        print()

        if dry_run:
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
    if keep_chroma:
        print(f"Kept ChromaDB collections at {chroma_dir} (--keep-chroma).")
    elif chroma_dir.exists():
        shutil.rmtree(chroma_dir)
        print(f"Removed {chroma_dir}.")
    else:
        # Do not fall through quietly. The SQL rows are already gone; finding no
        # vector store almost always means this process resolved a DIFFERENT
        # root than the server writes to, and the two halves of the KB have just
        # been left inconsistent.
        print()
        print("⚠️  WARNING: no ChromaDB directory found at the resolved path:")
        print(f"      {chroma_dir}")
        print("    knowledge_items rows were deleted but NO vector collections")
        print("    were removed, so SQL and the vector store may now DIVERGE —")
        print("    searches can still return chunks whose rows no longer exist.")
        print("    This usually means the server writes its store somewhere else")
        print("    (different PROJECT_ROOT, different working directory, or an")
        print("    external CHROMADB_URL, which this command does not touch).")
        print("    Compare the path above with the server's before continuing.")

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
            organization_id=SingleTenantProvider.DEFAULT_ORG_ID,
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
        help="Skip removing data/chroma-kb/ (default: remove).",
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
