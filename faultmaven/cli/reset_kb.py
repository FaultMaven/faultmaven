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

from sqlalchemy import delete, select

from faultmaven.bootstrap.data_init import get_project_root


async def _count_rows(session, model):
    result = await session.execute(select(model))
    return len(list(result.scalars().all()))


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

    async with get_db_session() as session:
        ki_count = await _count_rows(session, KnowledgeItemModel)
        draft_count = await _count_rows(session, ConversionDraftModel)

    print("Current state:")
    print(f"  knowledge_items rows: {ki_count}")
    print(f"  conversion_drafts rows: {draft_count}")
    print(f"  data/chroma-kb/ exists: {chroma_dir.exists()}")
    print()

    if dry_run:
        print("(dry-run) No changes made.")
        return 0

    # SQL wipe
    async with get_db_session() as session:
        await session.execute(delete(KnowledgeItemModel))
        if all_drafts:
            await session.execute(delete(ConversionDraftModel))
            print(f"Deleted {draft_count} conversion_drafts rows (--all-drafts).")
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
    print(f"Deleted {ki_count} knowledge_items rows.")

    # ChromaDB wipe
    if not keep_chroma and chroma_dir.exists():
        shutil.rmtree(chroma_dir)
        print(f"Removed {chroma_dir}.")
    elif keep_chroma:
        print("Kept ChromaDB collections (--keep-chroma).")

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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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

    # Exit explicitly rather than returning the code: the console-script wrapper
    # is what would otherwise carry it, and `python -m faultmaven.cli.reset_kb`
    # would silently drop a non-zero status.
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
