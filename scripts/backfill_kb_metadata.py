#!/usr/bin/env python3
"""Backfill KB chunk metadata with RAG enrichment fields.

Re-reads all runbook source files, extracts frontmatter (domain, service,
last_updated, status), and re-indexes them into ChromaDB with the new
structure-aware chunking and enriched metadata.

This is a one-time migration script. Run it after deploying the RAG revamp
to update the 59 built-in runbooks (and any user-uploaded runbooks that have
source files on disk).

Usage:
    python scripts/backfill_kb_metadata.py [--dry-run]

What it does:
  1. Scans data/knowledge/ for all .md runbook files
  2. For each file, extracts frontmatter metadata
  3. Deletes old chunks from ChromaDB (by document_id)
  4. Re-indexes with structure-aware chunking and enriched metadata

Flags:
  --dry-run   Show what would be done without modifying ChromaDB
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_kb_metadata")


async def main(dry_run: bool = False):
    from faultmaven.config.settings import get_settings
    from faultmaven.core.knowledge.ingestion import KnowledgeIngester
    from faultmaven.utils.frontmatter import extract_frontmatter_metadata

    settings = get_settings()
    ingester = KnowledgeIngester(settings=settings)

    # Find all runbook files
    knowledge_dirs = [
        Path("data/knowledge/global"),
        Path("resources/knowledge/builtin"),
    ]

    # Also scan personal/team dirs
    data_kb = Path("data/knowledge")
    if data_kb.exists():
        for subdir in data_kb.iterdir():
            if subdir.is_dir() and subdir.name not in ("global", "sources"):
                knowledge_dirs.append(subdir)

    files_found = []
    for kb_dir in knowledge_dirs:
        if not kb_dir.exists():
            continue
        for md_file in sorted(kb_dir.rglob("*.md")):
            if "sources" in md_file.parts:
                continue
            files_found.append(md_file)

    logger.info(f"Found {len(files_found)} runbook files to re-index")

    if not files_found:
        logger.info("No files found. Nothing to do.")
        return

    # Parse frontmatter to get document IDs
    import re
    import yaml

    reindexed = 0
    skipped = 0
    errors = []

    for md_file in files_found:
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as e:
            errors.append(f"{md_file.name}: cannot read ({e})")
            continue

        # Extract frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not fm_match:
            skipped += 1
            logger.debug(f"Skipping {md_file.name}: no frontmatter")
            continue

        try:
            metadata = yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            skipped += 1
            logger.debug(f"Skipping {md_file.name}: invalid YAML")
            continue

        doc_id = metadata.get("id", md_file.stem)
        title = metadata.get("title", md_file.stem.replace("-", " ").title())

        # Infer scope from path
        scope = "global"
        try:
            rel = md_file.relative_to(data_kb) if str(md_file).startswith(str(data_kb)) else None
            if rel and len(rel.parts) > 1:
                scope_dir = rel.parts[0]
                if scope_dir.startswith("personal_") or scope_dir.startswith("user_"):
                    scope = "personal"
                elif scope_dir.startswith("team_"):
                    scope = "team"
        except ValueError:
            pass

        rag_meta = extract_frontmatter_metadata(content)

        if dry_run:
            logger.info(
                f"[DRY RUN] Would re-index: {doc_id} ({title}) "
                f"scope={scope} domain={rag_meta.get('domain', 'N/A')} "
                f"service={rag_meta.get('service', 'N/A')} "
                f"status={rag_meta.get('status', 'N/A')} "
                f"last_updated={rag_meta.get('last_updated', 'N/A')}"
            )
            reindexed += 1
            continue

        # Delete old chunks
        try:
            deleted = await ingester.delete_document(doc_id)
            if deleted:
                logger.debug(f"Deleted old chunks for {doc_id}")
        except Exception as e:
            logger.warning(f"Failed to delete old chunks for {doc_id}: {e}")

        # Re-ingest with new chunking and metadata
        try:
            await ingester.ingest_document(
                file_path=str(md_file),
                title=title,
                document_type=metadata.get("document_type", "runbook"),
                tags=metadata.get("tags", []),
                source_url=metadata.get("source_url"),
                document_id=doc_id,
                scope=scope,
                owner_id=metadata.get("owner_id"),
                team_id=metadata.get("team_id"),
            )
            reindexed += 1
            logger.info(f"Re-indexed: {doc_id} ({title})")
        except Exception as e:
            errors.append(f"{md_file.name}: ingestion failed ({e})")
            logger.error(f"Failed to re-index {doc_id}: {e}")

    # Summary
    logger.info(
        f"\n{'DRY RUN ' if dry_run else ''}Backfill complete:\n"
        f"  Re-indexed: {reindexed}\n"
        f"  Skipped:    {skipped}\n"
        f"  Errors:     {len(errors)}"
    )
    if errors:
        logger.warning("Errors:")
        for err in errors:
            logger.warning(f"  - {err}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill KB metadata for RAG")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without modifying ChromaDB",
    )
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))
