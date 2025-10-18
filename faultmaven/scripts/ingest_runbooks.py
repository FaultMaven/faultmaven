"""
Runbook Ingestion Pipeline
Automatically discovers, validates, and ingests runbooks into ChromaDB

Features:
- YAML frontmatter parsing for metadata
- Technology tag extraction
- Validation against schema
- Batch ingestion with progress tracking
- Incremental updates (only ingest changed runbooks)
- Verification reporting

Usage:
    python -m faultmaven.scripts.ingest_runbooks
    python -m faultmaven.scripts.ingest_runbooks --runbook-dir docs/runbooks --verify
    python -m faultmaven.scripts.ingest_runbooks --technology kubernetes --force-reingest
"""

import asyncio
import argparse
import glob
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone
from faultmaven.utils.serialization import to_json_compatible

try:
    import yaml
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.table import Table
except ImportError:
    print("Error: Required packages not installed.")
    print("Please install: pip install pyyaml rich")
    sys.exit(1)

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from faultmaven.core.knowledge.ingestion import KnowledgeIngester

logger = logging.getLogger(__name__)
console = Console()


class RunbookValidator:
    """Validates runbooks against quality standards"""

    REQUIRED_METADATA_FIELDS = [
        "id", "title", "technology", "severity", "tags",
        "difficulty", "version", "last_updated", "verified_by", "status"
    ]

    REQUIRED_SECTIONS = [
        "Quick Reference Card",
        "Diagnostic Steps",
        "Solutions",
        "Prevention",
        "Related Issues"
    ]

    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate_runbook(self, file_path: str, content: str, metadata: Dict) -> bool:
        """Validate runbook structure and content"""
        self.errors = []
        self.warnings = []

        # Check metadata completeness
        for field in self.REQUIRED_METADATA_FIELDS:
            if field not in metadata:
                self.errors.append(f"Missing required metadata field: {field}")

        # Check status
        if metadata.get("status") not in ["verified", "draft", "deprecated"]:
            self.errors.append(f"Invalid status: {metadata.get('status')}")

        # Check required sections
        for section in self.REQUIRED_SECTIONS:
            if f"## {section}" not in content and f"# {section}" not in content:
                self.errors.append(f"Missing required section: {section}")

        # Check for code blocks
        if "```bash" not in content and "```" not in content:
            self.warnings.append("No code blocks found - runbook should include commands")

        return len(self.errors) == 0


class RunbookIngestionPipeline:
    """Automated pipeline for runbook ingestion"""

    def __init__(self, runbook_dir: str = "docs/runbooks"):
        self.runbook_dir = Path(runbook_dir)
        self.ingester: Optional[KnowledgeIngester] = None
        self.validator = RunbookValidator()
        self.ingestion_log_file = self.runbook_dir / ".ingestion_log.json"
        self.ingestion_log: Dict[str, Dict] = self._load_ingestion_log()

    def _load_ingestion_log(self) -> Dict:
        """Load log of previously ingested runbooks"""
        if self.ingestion_log_file.exists():
            try:
                with open(self.ingestion_log_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load ingestion log: {e}")
        return {}

    def _save_ingestion_log(self):
        """Save ingestion log"""
        try:
            with open(self.ingestion_log_file, 'w') as f:
                json.dump(self.ingestion_log, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save ingestion log: {e}")

    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate MD5 hash of file content for change detection"""
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()

    def discover_runbooks(
        self,
        technology: Optional[str] = None,
        status_filter: Optional[str] = None
    ) -> List[Path]:
        """Discover all runbook markdown files"""
        pattern = "**/*.md"
        if technology:
            pattern = f"{technology}/**/*.md"

        runbook_files = []
        for file_path in self.runbook_dir.glob(pattern):
            # Skip special files
            if file_path.name in ["README.md", "TEMPLATE.md", "CONTRIBUTING.md", "REVIEW_GUIDELINES.md"]:
                continue

            # Filter by status if requested
            if status_filter:
                metadata = self._extract_metadata(file_path)
                if metadata.get("status") != status_filter:
                    continue

            runbook_files.append(file_path)

        return runbook_files

    def _extract_metadata(self, file_path: Path) -> Dict:
        """Extract YAML frontmatter metadata from runbook"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Parse YAML frontmatter
            if content.startswith('---'):
                parts = content.split('---', 2)
                if len(parts) >= 3:
                    try:
                        metadata = yaml.safe_load(parts[1])
                        return metadata or {}
                    except yaml.YAMLError as e:
                        logger.warning(f"Failed to parse YAML in {file_path}: {e}")
        except Exception as e:
            logger.error(f"Error extracting metadata from {file_path}: {e}")

        return {}

    def _needs_reingestion(self, file_path: Path, force: bool = False) -> bool:
        """Check if runbook needs to be reingested"""
        if force:
            return True

        file_key = str(file_path.relative_to(self.runbook_dir))
        current_hash = self._calculate_file_hash(file_path)

        if file_key not in self.ingestion_log:
            return True

        previous_hash = self.ingestion_log[file_key].get("hash")
        return current_hash != previous_hash

    async def ingest_runbook(
        self,
        file_path: Path,
        validate: bool = True,
        dry_run: bool = False
    ) -> Dict[str, any]:
        """Ingest a single runbook"""
        result = {
            "file": str(file_path),
            "success": False,
            "errors": [],
            "warnings": [],
            "document_id": None
        }

        try:
            # Read runbook content
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Extract metadata
            metadata = self._extract_metadata(file_path)

            # Validate if requested
            if validate:
                is_valid = self.validator.validate_runbook(str(file_path), content, metadata)
                result["errors"] = self.validator.errors
                result["warnings"] = self.validator.warnings

                if not is_valid:
                    logger.error(f"Validation failed for {file_path}")
                    return result

            # Skip if dry run
            if dry_run:
                result["success"] = True
                result["dry_run"] = True
                return result

            # Ingest into knowledge base
            document_id = metadata.get("id", f"runbook_{file_path.stem}")
            title = metadata.get("title", file_path.stem)
            technology = metadata.get("technology", "general")
            tags = metadata.get("tags", [])

            # Add technology and difficulty as tags
            if technology and technology not in tags:
                tags.append(technology)
            difficulty = metadata.get("difficulty")
            if difficulty and difficulty not in tags:
                tags.append(difficulty)

            # Use runbook's last_updated from metadata, or current time
            last_updated = metadata.get("last_updated", "")
            if last_updated and isinstance(last_updated, str):
                # Convert date string like "2025-01-15" to ISO timestamp
                try:
                    dt = datetime.fromisoformat(last_updated)
                    updated_at = to_json_compatible(dt)
                except ValueError:
                    updated_at = to_json_compatible(to_json_compatible(datetime.now(timezone.utc))
            else:
                updated_at = to_json_compatible(to_json_compatible(datetime.now(timezone.utc))

            created_at = updated_at  # Use same timestamp for both

            await self.ingester.ingest_document(
                file_path=str(file_path),
                title=title,
                document_type="runbook",
                tags=tags,
                source_url=None,
                document_id=document_id,
                created_at=created_at,
                updated_at=updated_at
            )

            # Update ingestion log
            file_key = str(file_path.relative_to(self.runbook_dir))
            self.ingestion_log[file_key] = {
                "hash": self._calculate_file_hash(file_path),
                "ingested_at": to_json_compatible(datetime.now(timezone.utc).isoformat(),
                "document_id": document_id,
                "title": title,
                "technology": technology,
                "status": metadata.get("status", "unknown")
            }

            result["success"] = True
            result["document_id"] = document_id

        except Exception as e:
            logger.error(f"Failed to ingest {file_path}: {e}")
            result["errors"].append(str(e))

        return result

    async def run_pipeline(
        self,
        technology: Optional[str] = None,
        status_filter: str = "verified",
        force: bool = False,
        validate: bool = True,
        dry_run: bool = False
    ):
        """Run the complete ingestion pipeline"""
        console.print("\n[bold blue]FaultMaven Runbook Ingestion Pipeline[/bold blue]")
        console.print(f"Runbook directory: {self.runbook_dir}\n")

        # Initialize ingester
        if not dry_run:
            console.print("[yellow]Initializing knowledge base connection...[/yellow]")
            try:
                from faultmaven.config.settings import get_settings
                settings = get_settings()
                self.ingester = KnowledgeIngester(settings=settings)
                console.print("[green]✓ Connected to knowledge base[/green]\n")
            except Exception as e:
                console.print(f"[red]✗ Failed to connect to knowledge base: {e}[/red]")
                console.print("[yellow]Run with --dry-run to validate without ingesting[/yellow]")
                return

        # Discover runbooks
        console.print(f"[yellow]Discovering runbooks (technology={technology or 'all'}, status={status_filter or 'all'})...[/yellow]")
        runbooks = self.discover_runbooks(technology=technology, status_filter=status_filter)
        console.print(f"Found [green]{len(runbooks)}[/green] runbooks\n")

        if not runbooks:
            console.print("[yellow]No runbooks found to ingest[/yellow]")
            return

        # Filter by what needs reingestion
        if not force:
            runbooks_to_ingest = [rb for rb in runbooks if self._needs_reingestion(rb, force)]
            if len(runbooks_to_ingest) < len(runbooks):
                console.print(f"[green]{len(runbooks) - len(runbooks_to_ingest)}[/green] runbooks already up-to-date")
        else:
            runbooks_to_ingest = runbooks

        if not runbooks_to_ingest:
            console.print("[green]All runbooks are up-to-date![/green]")
            return

        console.print(f"[yellow]Ingesting {len(runbooks_to_ingest)} runbooks...[/yellow]\n")

        # Ingest with progress tracking
        results = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            task = progress.add_task("Ingesting runbooks...", total=len(runbooks_to_ingest))

            for runbook_path in runbooks_to_ingest:
                progress.update(task, description=f"Ingesting {runbook_path.name}...")
                result = await self.ingest_runbook(
                    runbook_path,
                    validate=validate,
                    dry_run=dry_run
                )
                results.append(result)
                progress.advance(task)

        # Save ingestion log
        if not dry_run:
            self._save_ingestion_log()

        # Generate report
        self._print_report(results, dry_run)

    def _print_report(self, results: List[Dict], dry_run: bool):
        """Print ingestion report"""
        console.print("\n[bold green]Ingestion Report[/bold green]\n")

        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]
        warnings = [r for r in results if r.get("warnings")]

        # Summary table
        table = Table(title="Summary")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", style="magenta")

        table.add_row("Total Runbooks", str(len(results)))
        table.add_row("Successful", f"[green]{len(successful)}[/green]")
        table.add_row("Failed", f"[red]{len(failed)}[/red]")
        table.add_row("Warnings", f"[yellow]{len(warnings)}[/yellow]")
        if dry_run:
            table.add_row("Mode", "[yellow]DRY RUN (validation only)[/yellow]")

        console.print(table)

        # Failures
        if failed:
            console.print("\n[bold red]Failed Runbooks:[/bold red]")
            for result in failed:
                console.print(f"  • {result['file']}")
                for error in result.get("errors", []):
                    console.print(f"    - [red]{error}[/red]")

        # Warnings
        if warnings:
            console.print("\n[bold yellow]Runbooks with Warnings:[/bold yellow]")
            for result in warnings:
                if result.get("warnings"):
                    console.print(f"  • {result['file']}")
                    for warning in result["warnings"]:
                        console.print(f"    - [yellow]{warning}[/yellow]")


async def main():
    parser = argparse.ArgumentParser(description="FaultMaven Runbook Ingestion Pipeline")
    parser.add_argument(
        "--runbook-dir",
        default="docs/runbooks",
        help="Directory containing runbooks"
    )
    parser.add_argument(
        "--technology",
        help="Filter by technology (e.g., kubernetes, redis)"
    )
    parser.add_argument(
        "--status",
        default="verified",
        choices=["verified", "draft", "deprecated", "all"],
        help="Filter by runbook status"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force reingest all runbooks (ignore change detection)"
    )
    parser.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip validation checks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without actually ingesting"
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Run pipeline
    pipeline = RunbookIngestionPipeline(runbook_dir=args.runbook_dir)

    await pipeline.run_pipeline(
        technology=args.technology,
        status_filter=None if args.status == "all" else args.status,
        force=args.force,
        validate=not args.no_validate,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    asyncio.run(main())
